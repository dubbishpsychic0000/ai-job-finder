"""Evidence-based recipient verification for employer communications."""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from app import memory as mem

EMAIL_RE = re.compile(r"(?<![\w.+-])([A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})(?![\w.+-])", re.I)
PLACEHOLDER_DOMAINS = {"example.com", "example.org", "example.net", "test.com", "test.org", "placeholder.com", "fake.com", "noreply.com", "no-reply.com"}
PLACEHOLDER_MARKERS = ("example", "test", "placeholder", "fake", "your-email", "email@")


@dataclass(frozen=True)
class Verification:
    email: str = ""
    source_url: str = ""
    source_domain: str = ""
    verified: bool = False
    verification_method: str = ""
    confidence: int = 0
    reason: str = ""


def is_safe_email(email: str) -> bool:
    value = (email or "").strip().lower()
    if not EMAIL_RE.fullmatch(value):
        return False
    local, domain = value.rsplit("@", 1)
    return domain not in PLACEHOLDER_DOMAINS and not any(marker in value for marker in PLACEHOLDER_MARKERS) and local not in {"noreply", "no-reply"}


class EmailVerificationService:
    """Stores a confidence-ranked audit record; never manufactures addresses."""

    def __init__(self, session: Session | None = None):
        self.session = session

    def extract(self, text: str) -> list[str]:
        return list(dict.fromkeys(email.lower() for email in EMAIL_RE.findall(text or "") if is_safe_email(email)))

    def verify(self, email: str, *, source_url: str = "", source_type: str = "",
               official: bool = False, job_id: int | None = None) -> Verification:
        email = (email or "").strip().lower()
        domain = urlparse(source_url).netloc.lower().split(":")[0]
        if not is_safe_email(email):
            result = Verification(email=email, source_url=source_url, source_domain=domain,
                                  reason="placeholder, test, malformed, or no-reply email")
        elif official or source_type in {"ats", "company_career"}:
            method = "official_employer_posting" if source_type in {"ats", "company_career"} else "official_company_page"
            result = Verification(email, source_url, domain, True, method, 100)
        elif source_type == "recruitment":
            result = Verification(email, source_url, domain, True, "trusted_recruitment_agency", 70)
        else:
            result = Verification(email, source_url, domain, False, "unverified_source", 20,
                                  "address is not on an official employer or trusted recruiter source")
        if self.session is not None:
            mem.store.add_email_verification(self.session, job_id=job_id, email=result.email,
                                             source_url=result.source_url, source_domain=result.source_domain,
                                             verified=result.verified, verification_method=result.verification_method,
                                             confidence=result.confidence)
        return result

    def verify_job(self, job, *, source_type: str = "") -> Verification:
        source_type = source_type or getattr(job, "source_type", "")
        candidates = self.extract(getattr(job, "description", ""))
        stored = getattr(job, "contact_email", "")
        if stored and stored not in candidates:
            candidates.insert(0, stored)
        if not candidates:
            return Verification(reason="no email found")
        # First verifiable address wins; a fake/example address never does.
        for address in candidates:
            result = self.verify(address, source_url=getattr(job, "url", ""), source_type=source_type,
                                 job_id=getattr(job, "id", None))
            if result.verified:
                return result
        return self.verify(candidates[0], source_url=getattr(job, "url", ""), source_type=source_type,
                           job_id=getattr(job, "id", None))
