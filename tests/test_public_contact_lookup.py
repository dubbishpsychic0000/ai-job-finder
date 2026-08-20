"""Public contact lookup is allowed only for official ATS/career pages."""
from __future__ import annotations

from app.discovery.email_verification import EmailVerificationService


class _Response:
    ok = True
    status_code = 200
    headers = {"Content-Type": "text/html"}
    text = "<p>Recruitment: talent@official-employer.com</p>"


class _Job:
    id = None
    description = ""
    contact_email = ""
    url = "https://jobs.lever.co/employer/123"


def test_official_ats_page_can_supply_a_visible_contact(monkeypatch):
    monkeypatch.setattr("app.discovery.email_verification.requests.get", lambda *a, **k: _Response())
    result = EmailVerificationService().verify_job(_Job(), source_type="ats")
    assert result.verified
    assert result.email == "talent@official-employer.com"
    assert result.verification_method == "official_employer_posting"


def test_search_result_page_is_never_fetched_for_contacts(monkeypatch):
    called = False

    def unexpected(*_a, **_k):
        nonlocal called
        called = True
        return _Response()

    monkeypatch.setattr("app.discovery.email_verification.requests.get", unexpected)
    result = EmailVerificationService().verify_job(_Job(), source_type="search_engine")
    assert not result.verified
    assert not called
