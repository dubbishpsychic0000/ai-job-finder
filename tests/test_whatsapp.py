from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app import models
from app.memory.store import get_or_create_company, opportunity_id
from app.notifications.service import NotificationService
from app.notifications.whatsapp import MetaWhatsAppProvider, WhatsAppSettings


class _Response:
    ok = True
    status_code = 200

    @staticmethod
    def json():
        return {"messages": [{"id": "wamid.test"}]}


def test_meta_whatsapp_uses_template_body_parameter():
    captured = {}

    def post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return _Response()

    provider = MetaWhatsAppProvider(WhatsAppSettings(
        access_token="secret-token", phone_number_id="1189875497552844",
        recipient="212600000000", template_name="job_digest"), post=post)
    assert provider.send("New opportunity")
    assert captured["url"].endswith("/1189875497552844/messages")
    assert captured["json"]["to"] == "212600000000"
    assert captured["json"]["template"]["name"] == "job_digest"
    assert captured["json"]["template"]["components"][0]["parameters"][0]["text"] == "New opportunity"


def test_unconfigured_provider_does_not_send():
    assert not MetaWhatsAppProvider(WhatsAppSettings()).send("No delivery")


def test_whatsapp_configuration_validation_never_sends():
    settings = WhatsAppSettings()
    errors = settings.validation_errors()
    assert "WHATSAPP_ACCESS_TOKEN is missing" in errors
    assert "WHATSAPP_PHONE_NUMBER_ID is missing" in errors
    assert "WHATSAPP_RECIPIENT is missing" in errors
    assert "WHATSAPP_TEMPLATE_NAME is missing" in errors
    assert not WhatsAppSettings(
        access_token="x" * 32,
        phone_number_id="123456789",
        recipient="+212600000000",
        template_name="portal_alert_fr",
        template_language="fr",
    ).validation_errors()


def test_whatsapp_configuration_rejects_malformed_values():
    settings = WhatsAppSettings(
        access_token="short token",
        phone_number_id="id-with-letters",
        recipient="0600000000",
        template_name="Invalid Template Name",
        graph_api_version="latest",
        template_language="French",
    )
    errors = settings.validation_errors()
    assert any("WHATSAPP_ACCESS_TOKEN" in error for error in errors)
    assert any("WHATSAPP_PHONE_NUMBER_ID" in error for error in errors)
    assert any("WHATSAPP_RECIPIENT" in error for error in errors)
    assert any("WHATSAPP_TEMPLATE_NAME" in error for error in errors)
    assert any("WHATSAPP_GRAPH_API_VERSION" in error for error in errors)
    assert any("WHATSAPP_TEMPLATE_LANGUAGE" in error for error in errors)


def test_meta_whatsapp_normalizes_template_parameter_whitespace():
    captured = {}

    def post(_url, **kwargs):
        captured.update(kwargs)
        return _Response()

    provider = MetaWhatsAppProvider(WhatsAppSettings(
        access_token="secret-token", phone_number_id="id", recipient="212600000000",
        template_name="job_digest"), post=post)
    assert provider.send("Run complete\nJobs: 1\tErrors: 0")
    text = captured["json"]["template"]["components"][0]["parameters"][0]["text"]
    assert text == "Run complete Jobs: 1 Errors: 0"


def test_meta_whatsapp_normalizes_e164_recipient():
    captured = {}

    def post(_url, **kwargs):
        captured.update(kwargs)
        return _Response()

    provider = MetaWhatsAppProvider(WhatsAppSettings(
        access_token="secret-token", phone_number_id="id", recipient="+212 600-000-000",
        template_name="job_digest"), post=post)
    assert provider.send("test")
    assert captured["json"]["to"] == "212600000000"


def _portal_job(db):
    company = get_or_create_company(db, "Al Omrane Fès-Meknès", country="Morocco")
    closing_at = datetime(2026, 10, 23, 22, 59, tzinfo=timezone.utc)
    observed = datetime.now(timezone.utc)
    notice_url = "https://www.emploi-public.ma/fr/concours/download/arrete/example"
    listing_url = "https://www.emploi-public.ma/fr/concours/details/example"
    job = models.Job(
        source="emploi_public_ma",
        source_type="government",
        external_id="C44041/26",
        dedup_key="emploi_public_ma:C44041/26",
        title="Techniciens en Génie Civil",
        company_id=company.id,
        country="Morocco",
        location="Fès-Meknès",
        description="Two civil engineering technician positions; diploma and mobility required.",
        url=listing_url,
        entity_evidence_url=listing_url,
        closing_at=closing_at,
        status="new",
        entity_type="INDIVIDUAL_VACANCY",
        entity_state="VACANCY_VERIFIED",
        entity_reason_code="official_public_competition_verified_open",
        verification_status="verified",
        last_verified_at=observed,
        application_method="COMPANY_PORTAL",
        application_url="https://alomrane.gov.ma/Le-groupe/Offre-d-emplois",
        public_competition={
            "source": "emploi_public_ma",
            "status": "OPEN_ANNOUNCEMENT",
            "stage": "OPEN_ANNOUNCEMENT",
            "observed_at": observed.isoformat(),
            "evidence_url": listing_url,
            "reference": "C44041/26",
            "official_portal_verified": True,
            "qualification_requirements": "Technicien spécialisé en génie civil",
            "eligibility_requirements": "Moroccan nationality; civil-engineering diploma.",
            "eligibility_assessment": {
                "relevant": True,
                "requirements_summary": "Moroccan nationality; civil-engineering diploma.",
                "summary": "Nationality confirmed; qualification appears aligned; mobility uncertain.",
            },
            "required_documents": [
                {"name": "Current CV", "available_locally": True},
                {"name": "Diploma copy", "available_locally": False},
            ],
            "submission_route": {
                "kind": "ONLINE_PORTAL",
                "destination_url": "https://alomrane.gov.ma/Le-groupe/Offre-d-emplois",
                "evidence_url": notice_url,
                "conflict": False,
            },
        },
    )
    db.add(job)
    db.flush()
    return job


def test_portal_alert_is_sent_once_to_configured_whatsapp_owner(db):
    job = _portal_job(db)
    service = NotificationService(db)

    class _Accepted:
        configured = True
        last_message_id = "wamid.portal"

        def __init__(self):
            self.messages = []

        def send(self, message):
            self.messages.append(message)
            return True

    provider = _Accepted()
    now = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)
    result = service.send_portal_application_alert(job, provider, now=now)
    duplicate = service.send_portal_application_alert(job, provider, now=now)

    assert result["status"] == "accepted_by_meta_delivery_unconfirmed"
    assert "You should apply here:" in result["message"]
    assert "23 October 2026" in result["message"]
    assert "Diploma copy" in result["message"]
    assert duplicate["status"] == "already_attempted"
    assert len(provider.messages) == 1
    alert = db.query(models.Notification).one()
    assert alert.status == "accepted"
    assert alert.payload["outcome"] == "accepted_by_meta_delivery_unconfirmed"


def test_portal_alert_configuration_and_delivery_failures_are_recorded_without_retry(db):
    job = _portal_job(db)
    service = NotificationService(db)

    class _Unconfigured:
        configured = False

        def send(self, _message):
            raise AssertionError("unconfigured transport must not be called")

    now = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)
    result = service.send_portal_application_alert(job, _Unconfigured(), now=now)
    assert result["status"] == "failed"
    assert result["reason"] == "whatsapp_not_configured"
    assert db.query(models.Notification).one().payload["outcome"] == "whatsapp_not_configured"

    class _Rejected:
        configured = True

        def send(self, _message):
            return False

    other_job = _portal_job(db)
    failed = service.send_portal_application_alert(other_job, _Rejected(), now=now)
    retry = service.send_portal_application_alert(other_job, _Rejected(), now=now)
    assert failed["status"] == "failed"
    assert failed["reason"] == "provider_delivery_failed"
    assert retry["status"] == "already_attempted"


def _recovery_evidence(job):
    metadata = job.public_competition
    return {
        "status": "OPEN_ANNOUNCEMENT",
        "stage": "OPEN_ANNOUNCEMENT",
        "competition_code": metadata["reference"],
        "evidence_url": job.url,
        "deadline_at": "2026-10-23T22:59:00+00:00",
        "submission_route": {
            "kind": "ONLINE_PORTAL",
            "destination_url": job.application_url,
            "conflict": False,
        },
    }


def _seed_unconfigured_alert(db, job):
    class _Unconfigured:
        configured = False

        def send(self, _message):
            raise AssertionError("provider must not be called")

    now = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)
    result = NotificationService(db).send_portal_application_alert(
        job, _Unconfigured(), now=now,
    )
    assert result["reason"] == "whatsapp_not_configured"
    return db.query(models.Notification).one()


class _RecoveryProvider:
    configured = True

    def __init__(self):
        self.messages = []
        self.last_message_id = "wamid.recovered"

    def send(self, message):
        self.messages.append(message)
        return True


def test_recovers_only_the_proven_pre_dispatch_whatsapp_configuration_failure(db):
    job = _portal_job(db)
    alert = _seed_unconfigured_alert(db, job)
    alert.payload.pop("provider_called", None)  # legacy record: reason is pre-dispatch
    db.commit()
    provider = _RecoveryProvider()
    now = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)

    result = NotificationService(db).recover_portal_application_alert(
        notification_id=alert.id,
        opportunity_id=opportunity_id(job),
        provider=provider,
        verifier=_recovery_evidence,
        now=now,
    )

    assert result["status"] == "accepted_by_meta_delivery_unconfirmed"
    assert len(provider.messages) == 1
    message = provider.messages[0]
    for expected in (
        "Al Omrane Fès-Meknès",
        "Techniciens en Génie Civil",
        "C44041/26",
        job.application_url,
        "23 October 2026",
        "diplôme",
        "CV",
        "manquante",
        "WCA ne l'a pas soumise",
    ):
        assert expected in message
    db.refresh(alert)
    assert alert.status == "accepted"
    assert alert.payload["provider_called"] is True
    assert alert.payload["recovery_count"] == 1
    assert alert.payload["provider_message_id"] == "wamid.recovered"


def test_recovery_deduplication_blocks_second_attempt(db):
    job = _portal_job(db)
    alert = _seed_unconfigured_alert(db, job)
    provider = _RecoveryProvider()
    service = NotificationService(db)
    kwargs = {
        "notification_id": alert.id,
        "opportunity_id": opportunity_id(job),
        "provider": provider,
        "verifier": _recovery_evidence,
        "now": datetime(2026, 10, 21, 12, tzinfo=timezone.utc),
    }

    first = service.recover_portal_application_alert(**kwargs)
    second = service.recover_portal_application_alert(**kwargs)

    assert first["status"] == "accepted_by_meta_delivery_unconfirmed"
    assert second == {
        "status": "blocked",
        "reason": "prior_outcome_not_proven_pre_dispatch",
    }
    assert len(provider.messages) == 1


def test_recovery_atomic_claim_blocks_competing_attempt(db, session_factory):
    job = _portal_job(db)
    alert = _seed_unconfigured_alert(db, job)
    provider = _RecoveryProvider()
    competing_results = []

    class _CompetingProvider(_RecoveryProvider):
        def send(self, message):
            competing_session = session_factory()
            try:
                competing_results.append(
                    NotificationService(
                        competing_session,
                    ).recover_portal_application_alert(
                        notification_id=alert.id,
                        opportunity_id=opportunity_id(job),
                        provider=self,
                        verifier=_recovery_evidence,
                        now=datetime(2026, 10, 21, 12, tzinfo=timezone.utc),
                    )
                )
            finally:
                competing_session.close()
            return super().send(message)

    provider = _CompetingProvider()
    result = NotificationService(db).recover_portal_application_alert(
        notification_id=alert.id,
        opportunity_id=opportunity_id(job),
        provider=provider,
        verifier=_recovery_evidence,
        now=datetime(2026, 10, 21, 12, tzinfo=timezone.utc),
    )

    assert result["status"] == "accepted_by_meta_delivery_unconfirmed"
    assert competing_results == [{
        "status": "blocked",
        "reason": "prior_outcome_not_proven_pre_dispatch",
    }]
    assert len(provider.messages) == 1


def test_recovery_rejects_stale_or_changed_official_vacancy(db):
    job = _portal_job(db)
    alert = _seed_unconfigured_alert(db, job)
    provider = _RecoveryProvider()
    evidence = _recovery_evidence(job)
    evidence["status"] = "CLOSED_ANNOUNCEMENT"
    result = NotificationService(db).recover_portal_application_alert(
        notification_id=alert.id,
        opportunity_id=opportunity_id(job),
        provider=provider,
        verifier=lambda _job: evidence,
        now=datetime(2026, 10, 21, 12, tzinfo=timezone.utc),
    )
    assert result["reason"] == "vacancy_or_route_not_currently_verified"
    assert provider.messages == []

    evidence["status"] = "OPEN_ANNOUNCEMENT"
    evidence["deadline_at"] = "2026-10-20T22:59:00+00:00"
    result = NotificationService(db).recover_portal_application_alert(
        notification_id=alert.id,
        opportunity_id=opportunity_id(job),
        provider=provider,
        verifier=lambda _job: evidence,
        now=datetime(2026, 10, 21, 12, tzinfo=timezone.utc),
    )
    assert result["reason"] == "vacancy_or_route_not_currently_verified"
    assert provider.messages == []


def test_recovery_rejects_malformed_configuration_and_wrong_alert_or_job(db):
    job = _portal_job(db)
    alert = _seed_unconfigured_alert(db, job)

    class _Malformed(_RecoveryProvider):
        settings = type("Settings", (), {
            "validation_errors": lambda self: ["invalid config"],
        })()

    malformed = _Malformed()
    result = NotificationService(db).recover_portal_application_alert(
        notification_id=alert.id,
        opportunity_id=opportunity_id(job),
        provider=malformed,
        verifier=_recovery_evidence,
    )
    assert result["reason"] == "whatsapp_configuration_invalid"
    assert malformed.messages == []

    result = NotificationService(db).recover_portal_application_alert(
        notification_id=alert.id + 100,
        opportunity_id=opportunity_id(job),
        provider=_RecoveryProvider(),
        verifier=_recovery_evidence,
    )
    assert result["reason"] == "alert_job_mismatch"
    assert NotificationService(db).recover_portal_application_alert(
        notification_id=alert.id,
        opportunity_id="JOB-2026-1021-0002",
        provider=_RecoveryProvider(),
        verifier=_recovery_evidence,
    )["reason"] == "job_not_found"


@pytest.mark.parametrize(
    ("status", "payload"),
    [
        ("accepted", {
            "outcome": "accepted_by_meta_delivery_unconfirmed",
            "provider_called": True,
        }),
        ("failed", {
            "outcome": "provider_delivery_ambiguous",
            "provider_called": True,
        }),
        ("failed", {
            "outcome": "provider_delivery_failed",
            "provider_called": True,
        }),
    ],
)
def test_recovery_never_retries_provider_accepted_or_attempted_outcomes(
    db, status, payload,
):
    job = _portal_job(db)
    alert = _seed_unconfigured_alert(db, job)
    alert.status = status
    alert.payload = payload
    db.commit()
    provider = _RecoveryProvider()
    result = NotificationService(db).recover_portal_application_alert(
        notification_id=alert.id,
        opportunity_id=opportunity_id(job),
        provider=provider,
        verifier=_recovery_evidence,
    )
    assert result["reason"] == "prior_outcome_not_proven_pre_dispatch"
    assert provider.messages == []


def test_portal_alert_never_routes_email_applications_to_whatsapp(db):
    job = _portal_job(db)
    job.application_method = "EMAIL"
    result = NotificationService(db).send_portal_application_alert(
        job, MetaWhatsAppProvider(WhatsAppSettings()),
    )
    assert result == {
        "status": "skipped",
        "reason": "verified_relevant_portal_evidence_required",
    }
    assert db.query(models.Notification).count() == 0


def test_verified_ats_portal_alert_uses_whatsapp_without_email(db, prefs):
    job = _portal_job(db)
    job.source = "greenhouse"
    job.source_type = "ats"
    job.external_id = "role-901"
    job.url = "https://boards.greenhouse.io/example/jobs/901"
    job.application_url = job.url
    job.entity_evidence_url = job.url
    job.entity_reason_code = "matching_public_ats_posting_record"
    job.public_competition = {}
    job.description = "Verified portal listing for a civil engineering technician."
    db.add(models.JobAnalysis(
        job_id=job.id,
        skills_required=["civil engineering"],
        education_required="",
        experience_min=None,
    ))
    db.add(models.Decision(
        job_id=job.id,
        decision="APPLY",
        overall_score=90,
        scores={},
        reason="Candidate meets the assessed requirements.",
        rules_fired=[],
    ))
    db.flush()

    class _Accepted:
        configured = True
        last_message_id = "wamid.ats"

        def __init__(self):
            self.messages = []

        def send(self, message):
            self.messages.append(message)
            return True

    provider = _Accepted()
    # The ATS portal URL is the official destination; no email route exists.
    job.application_method = "COMPANY_PORTAL"
    result = NotificationService(db).send_portal_application_alert(
        job, provider, preferences=prefs,
    )

    assert result["status"] == "accepted_by_meta_delivery_unconfirmed"
    assert len(provider.messages) == 1
    assert "boards.greenhouse.io" in provider.messages[0]
    assert db.query(models.Email).count() == 0
