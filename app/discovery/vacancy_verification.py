"""Evidence-backed verification for supported public ATS detail records."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlsplit

import requests

from app.connectors.base import infer_country, parse_date
from app.discovery.opportunity_details import explicit_email_application_recipient

USER_AGENT = "WorldwideCareerAgent/0.1 (public vacancy verification)"
MAX_DETAIL_VERIFICATIONS_PER_RUN = 12
_GREENHOUSE_URL = re.compile(
    r"^/(?:[^/]+/)?jobs/(\d+)(?:/[^/]*)?/?$", re.IGNORECASE,
)
_SMARTRECRUITERS_URL = re.compile(
    r"^/([^/]+)/(\d+)(?:-[^/]*)?/?$", re.IGNORECASE,
)
_GENERIC_TITLES = {
    "career", "careers", "job", "jobs", "job opening", "job opportunity",
    "open position", "open positions", "career opportunities",
    "current openings", "all jobs",
}
_GENERIC_EMPLOYERS = {"", "unknown", "n/a", "na", "not available", "confidential"}
_CLOSED_STATUS = {
    "closed", "expired", "inactive", "unpublished", "withdrawn",
    "cancelled", "canceled", "deleted",
}
_OPEN_STATUS = {"open", "active", "published", "live"}


@dataclass
class VacancyEvidence:
    outcome: str
    reason_code: str
    evidence_url: str
    observed_at: datetime
    title: str = ""
    employer: str = ""
    location: str = ""
    country: str = ""
    description: str = ""
    posted_at: datetime | None = None
    posted_at_source: str = ""
    closing_at: datetime | None = None
    application_url: str = ""
    application_method: str = ""
    application_email: str = ""
    evidence_checks: dict[str, bool] = field(default_factory=dict)


def supported_ats_detail(url: str) -> tuple[str, str, str] | None:
    """Return safe API coordinates only for exact, recognized detail URL shapes."""
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in {None, 443}
        ):
            return None
    except ValueError:
        return None
    host = (parsed.hostname or "").casefold()
    parts = parsed.path
    if host in {"boards.greenhouse.io", "job-boards.greenhouse.io"}:
        match = _GREENHOUSE_URL.fullmatch(parts)
        if match:
            board = parts.strip("/").split("/")[0]
            return "greenhouse", board, match.group(1)
    if host == "jobs.smartrecruiters.com":
        match = _SMARTRECRUITERS_URL.fullmatch(parts)
        if match:
            return "smartrecruiters", match.group(1), match.group(2)
    return None


def _clean_html(value: str) -> str:
    if "<" not in (value or ""):
        return re.sub(r"\s+", " ", value or "").strip()
    from bs4 import BeautifulSoup

    return BeautifulSoup(value or "", "html.parser").get_text(" ", strip=True)


def _job_jsonld(html: str) -> dict:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html or "", "html.parser")
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.string or script.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        entries = payload if isinstance(payload, list) else [payload]
        pending = list(entries)
        while pending:
            entry = pending.pop(0)
            if not isinstance(entry, dict):
                continue
            graph = entry.get("@graph", [])
            pending.extend(item for item in graph if isinstance(item, dict))
            kind = entry.get("@type", "")
            if "JobPosting" in (
                kind if isinstance(kind, list) else [kind]
            ):
                return entry
    return {}


def _text(value) -> str:
    if isinstance(value, str):
        return re.sub(r"\s+", " ", _clean_html(value)).strip()
    if isinstance(value, dict):
        return _text(value.get("text") or value.get("value") or "")
    if isinstance(value, list):
        return " ".join(filter(None, (_text(item) for item in value)))
    return ""


def _location(value) -> str:
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, dict):
        return ""
    if value.get("name"):
        return _text(value.get("name"))
    address = value.get("address") if isinstance(value.get("address"), dict) else value
    return ", ".join(
        str(address.get(key) or "").strip()
        for key in ("addressLocality", "addressRegion", "addressCountry", "city", "country")
        if str(address.get(key) or "").strip()
    )


def _nested_text(payload: dict, *keys: str) -> str:
    value = payload
    for key in keys:
        if not isinstance(value, dict):
            return ""
        value = value.get(key)
    return _text(value)


class VacancyDetailVerifier:
    """Verify supported ATS jobs from their public, structured detail records.

    A successful HTTP response alone is never evidence of an open vacancy.
    The returned object must match the URL's stable ID and include role,
    employer, location, meaningful description, and the ATS's public active
    posting record.
    """

    def __init__(self, *, get=None, now=None):
        self.get = get or requests.get
        self.now = now or (lambda: datetime.now(timezone.utc))

    def verify(self, url: str) -> VacancyEvidence:
        observed_at = self.now()
        fallback = VacancyEvidence(
            "UNKNOWN", "unsupported_or_invalid_detail_url", url, observed_at,
        )
        coordinates = supported_ats_detail(url)
        if coordinates is None:
            return fallback
        ats, account, posting_id = coordinates
        if ats == "greenhouse":
            api_url = (
                "https://boards-api.greenhouse.io/v1/boards/"
                f"{account}/jobs/{posting_id}?content=true"
            )
        else:
            api_url = (
                "https://api.smartrecruiters.com/v1/companies/"
                f"{account}/postings/{posting_id}"
            )
        try:
            response = self.get(
                api_url,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                timeout=12,
                allow_redirects=False,
            )
        except requests.RequestException:
            return VacancyEvidence(
                "UNKNOWN", "ats_detail_unavailable", url, observed_at,
            )
        status = int(getattr(response, "status_code", 0) or 0)
        if status == 410:
            return VacancyEvidence("CLOSED", "ats_record_gone", url, observed_at)
        if status in {301, 302, 303, 307, 308}:
            return VacancyEvidence(
                "UNKNOWN", "ats_detail_redirect_unverified", url, observed_at,
            )
        if status == 404:
            return VacancyEvidence(
                "UNKNOWN", "ats_record_not_found_status_unknown", url, observed_at,
            )
        if status != 200:
            return VacancyEvidence(
                "UNKNOWN", "ats_detail_unavailable", url, observed_at,
            )
        try:
            payload = response.json()
        except (ValueError, TypeError):
            return VacancyEvidence(
                "UNKNOWN", "ats_detail_invalid_structured_data", url, observed_at,
            )
        if not isinstance(payload, dict):
            return VacancyEvidence(
                "UNKNOWN", "ats_detail_invalid_structured_data", url, observed_at,
            )

        result = (
            self._greenhouse(payload, url, posting_id, observed_at)
            if ats == "greenhouse"
            else self._smartrecruiters(payload, url, posting_id, observed_at)
        )
        return result

    def _greenhouse(
        self, payload: dict, url: str, posting_id: str, observed_at: datetime,
    ) -> VacancyEvidence:
        if str(payload.get("id") or "") != posting_id:
            return VacancyEvidence(
                "UNKNOWN", "ats_posting_id_mismatch", url, observed_at,
            )
        structured = _job_jsonld("")
        employer_value = payload.get("company_name") or payload.get("company")
        employer = _nested_text(employer_value, "name") if isinstance(
            employer_value, dict,
        ) else _text(employer_value)
        try:
            page = self.get(
                url,
                headers={"User-Agent": USER_AGENT, "Accept": "text/html"},
                timeout=12,
                allow_redirects=False,
            )
            original_detail_page_fetched = (
                int(getattr(page, "status_code", 0) or 0) == 200
            )
            if original_detail_page_fetched:
                structured = _job_jsonld(str(getattr(page, "text", "") or ""))
        except requests.RequestException:
            structured = {}
            original_detail_page_fetched = False
        if not employer:
            employer = _nested_text(structured, "hiringOrganization", "name")
        title = _text(payload.get("title")) or _text(structured.get("title"))
        location = _location(payload.get("location")) or _location(
            structured.get("jobLocation"),
        )
        description = _text(payload.get("content")) or _text(
            structured.get("description"),
        )
        posted_raw = structured.get("datePosted")
        closing_raw = structured.get("validThrough")
        route = _text(payload.get("absolute_url")) or url
        return self._assess(
            ats="greenhouse",
            payload=payload,
            url=url,
            posting_id=posting_id,
            observed_at=observed_at,
            title=title,
            employer=employer,
            location=location,
            description=description,
            posted_raw=posted_raw,
            posted_field="datePosted",
            closing_raw=closing_raw,
            application_url=route,
            original_detail_page_fetched=original_detail_page_fetched,
        )

    def _smartrecruiters(
        self, payload: dict, url: str, posting_id: str, observed_at: datetime,
    ) -> VacancyEvidence:
        if str(payload.get("id") or "") != posting_id:
            return VacancyEvidence(
                "UNKNOWN", "ats_posting_id_mismatch", url, observed_at,
            )
        job_ad = payload.get("jobAd")
        job_ad = job_ad if isinstance(job_ad, dict) else {}
        sections = job_ad.get("sections", [])
        sections = sections if isinstance(sections, list) else []
        description = " ".join(
            filter(None, (
                _text(section.get("title", "")) + ": "
                + _text(section.get("text", ""))
                for section in sections if isinstance(section, dict)
            )),
        )
        location_obj = payload.get("location") or {}
        location = _location(location_obj)
        if not location and isinstance(location_obj, dict):
            location = ", ".join(filter(None, (
                str(location_obj.get(key) or "").strip()
                for key in ("city", "region", "country")
            )))
        route = _text(payload.get("postingUrl")) or url
        return self._assess(
            ats="smartrecruiters",
            payload=payload,
            url=url,
            posting_id=posting_id,
            observed_at=observed_at,
            title=_text(payload.get("name")),
            employer=_nested_text(payload, "company", "name"),
            location=location,
            description=description,
            posted_raw=payload.get("releasedDate"),
            posted_field="releasedDate",
            closing_raw=payload.get("expirationDate"),
            application_url=route,
            original_detail_page_fetched=False,
        )

    def _assess(
        self, *, ats: str, payload: dict, url: str, posting_id: str,
        observed_at: datetime, title: str, employer: str, location: str,
        description: str, posted_raw, posted_field: str, closing_raw,
        application_url: str, original_detail_page_fetched: bool,
    ) -> VacancyEvidence:
        status = str(payload.get("status") or "").strip().casefold()
        posted_at = parse_date(str(posted_raw)) if posted_raw else None
        closing_at = parse_date(str(closing_raw)) if closing_raw else None
        valid_route = _valid_application_route(
            ats, application_url, url, posting_id,
        )
        application_email = explicit_email_application_recipient(description)
        application_method = "EMAIL" if application_email else "COMPANY_PORTAL"
        evidence_checks = {
            "employer": employer.casefold() not in _GENERIC_EMPLOYERS,
            "role": bool(title and title.casefold() not in _GENERIC_TITLES),
            "location": bool(location),
            "description_sufficient": (
                len(description) >= 80 and len(description.split()) >= 10
            ),
            "original_detail_page_reference": bool(url),
            "original_detail_page_fetched": original_detail_page_fetched,
            "posting_identity_matched": True,
            "current_status_recognized": status in _OPEN_STATUS | _CLOSED_STATUS,
            "publication_date": posted_at is not None,
            "deadline": closing_at is not None,
            "application_route_verified": valid_route,
        }
        if status in _CLOSED_STATUS:
            outcome, reason = "CLOSED", "ats_reports_closed_status"
        elif closing_at is not None and closing_at <= observed_at:
            outcome, reason = "CLOSED", "source_reported_closing_date_elapsed"
        elif status and status not in _OPEN_STATUS:
            outcome, reason = "UNKNOWN", "ats_status_unrecognized"
        elif (
            not evidence_checks["role"]
            or not evidence_checks["employer"]
            or not evidence_checks["location"]
            or not evidence_checks["description_sufficient"]
        ):
            outcome, reason = "UNKNOWN", "ats_record_missing_required_job_evidence"
        elif not valid_route:
            outcome, reason = "UNKNOWN", "ats_application_route_unverified"
        else:
            outcome, reason = "OPEN", "matching_public_ats_posting_record"

        return VacancyEvidence(
            outcome=outcome,
            reason_code=reason,
            evidence_url=url,
            observed_at=observed_at,
            title=title,
            employer=employer,
            location=location,
            country=infer_country(location),
            description=description[:20000],
            posted_at=posted_at,
            posted_at_source=(
                f"{ats}:{posted_field}" if posted_at is not None else ""
            ),
            closing_at=closing_at,
            application_url=application_url if outcome == "OPEN" else "",
            application_method=application_method if outcome == "OPEN" else "",
            application_email=application_email if outcome == "OPEN" else "",
            evidence_checks=evidence_checks,
        )


def _valid_application_route(
    ats: str, route: str, detail_url: str, posting_id: str,
) -> bool:
    try:
        parsed = urlsplit(route)
        detail = urlsplit(detail_url)
        route_port = parsed.port
    except ValueError:
        return False
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    if (
        parsed.username is not None
        or parsed.password is not None
        or route_port not in {None, 443}
    ):
        return False
    if ats == "greenhouse":
        host_ok = parsed.hostname in {
            "boards.greenhouse.io", "job-boards.greenhouse.io",
        }
    else:
        host_ok = parsed.hostname == "jobs.smartrecruiters.com"
    posting_path_match = any(
        part == posting_id or part.startswith(posting_id + "-")
        for part in parsed.path.strip("/").split("/")
    )
    return (
        host_ok and parsed.hostname == detail.hostname
        and posting_path_match
    )
