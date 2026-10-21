from __future__ import annotations

from datetime import datetime, timezone

from app import models
from app.memory.store import get_or_create_company
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


def test_verified_ats_portal_alert_uses_whatsapp_without_email(db):
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
        job, provider,
    )

    assert result["status"] == "accepted_by_meta_delivery_unconfirmed"
    assert len(provider.messages) == 1
    assert "boards.greenhouse.io" in provider.messages[0]
    assert db.query(models.Email).count() == 0
