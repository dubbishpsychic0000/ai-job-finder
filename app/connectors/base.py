"""Connector interface and the normalized Opportunity format.

Adapted from the spec:

    {
      "source": "example",
      "source_type": "company_career",
      "external_id": "12345",
      "title": "Civil Engineering Technician",
      "company": "Example Construction",
      "location": "France",
      "country": "FR",
      "description": "...",
      "url": "...",
      "posted_at": "...",
      "closing_at": null,
      "employment_type": "full_time",
      "salary": null,
      "contact_email": null,
      "international_candidate_signal": "unknown",
      "sponsorship_signal": "unknown"
    }
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

KNOWN_COUNTRIES = [
    "belgium", "canada", "france", "germany", "netherlands", "portugal",
    "spain", "switzerland", "luxembourg", "australia", "united kingdom",
    "uk", "usa", "united states", "ireland", "italy", "austria", "quebec",
]

# §2 — every source exposes a category so quality/confidence can be derived.
SOURCE_TYPES = (
    "job_board", "ats", "company_career", "government", "search_engine",
    "recruitment", "aggregator", "immigration", "employer", "social_signal",
    "unknown",
)

# §22 — default quality scores per source type (configurable in settings).
DEFAULT_SOURCE_QUALITY = {
    "company_career": 100,
    "employer": 100,
    "government": 100,
    "ats": 95,
    "recruitment": 90,
    "job_board": 85,
    "immigration": 80,
    "search_engine": 75,
    "social_signal": 60,
    "aggregator": 40,
    "unknown": 40,
}

# §34 — how a connector may legally access its source. Enforced as part of the
# connector interface (not just README guidance).
ACCESS_MODES = ("public", "authorized_only", "user_provided")

SIGNAL_UNKNOWN = "unknown"
SIGNALS = ("unknown", "high", "medium", "low", "no")

VERIFICATION_VERIFIED = "verified"
VERIFICATION_STATUSES = ("verified", "unverified", "stale", "closed")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def infer_country(location: str) -> str:
    """Best-effort country guess from a location string (also matches FR cities)."""
    loc = _norm(location).lower()
    if not loc:
        return ""
    for c in KNOWN_COUNTRIES:
        if c in loc:
            return c.title()
    # heuristics for common non-English forms
    if "canada" in loc or "québec" in loc or "quebec" in loc or loc.endswith(", ca"):
        return "Canada"
    if re.search(r"\b(maroc|morocco)\b", loc):
        return "Morocco"
    if re.search(r"\b(france|paris|lyon|marseille|toulouse|bordeaux|nantes|grenoble|lille|nice|rennes)\b", loc):
        return "France"
    if re.search(r"\b(belgique|belgië|belgium|bruxelles|liège|gent|antwerpen)\b", loc):
        return "Belgium"
    if re.search(r"\b(españa|espagne|spain|madrid|barcelona|sevilla|valencia)\b", loc):
        return "Spain"
    if re.search(r"\b(deutschland|germany|berlin|münchen|munich|hamburg|stuttgart)\b", loc):
        return "Germany"
    if re.search(r"\b(nederland|netherlands|amsterdam|rotterdam|utrecht)\b", loc):
        return "Netherlands"
    if re.search(r"\b(portugal|lisbon|lisboa|porto)\b", loc):
        return "Portugal"
    return ""


@dataclass
class Opportunity:
    source: str
    external_id: str
    title: str
    company: str = ""
    location: str = ""
    country: str = ""
    description: str = ""
    url: str = ""
    posted_at: datetime | None = None
    employment_type: str = "full_time"
    salary: str | None = None
    contact_email: str | None = None
    raw: dict = field(default_factory=dict)
    # Discovery V2 metadata (§2, §20, §22, §29)
    source_type: str = ""
    source_quality: int | None = None
    closing_at: datetime | None = None
    language: str = ""
    sponsorship_signal: str = SIGNAL_UNKNOWN
    international_candidate_signal: str = SIGNAL_UNKNOWN
    relocation_signal: str = SIGNAL_UNKNOWN
    work_permit_signal: str = SIGNAL_UNKNOWN
    verification_status: str = VERIFICATION_VERIFIED

    def effective_source_type(self) -> str:
        """Explicit source_type, else derived from the source name."""
        if self.source_type:
            return self.source_type
        return source_type_for(self.source)

    def effective_quality(self) -> int:
        return self.source_quality if self.source_quality is not None else DEFAULT_SOURCE_QUALITY.get(
            self.effective_source_type(), DEFAULT_SOURCE_QUALITY["unknown"])

    def canonical_job_id(self) -> str:
        """Cross-source canonical identity (§21) — title + company + country hash.

        All postings of the same vacancy (Indeed / company / ATS / agency) share
        this id regardless of source, so dedup can merge them.
        """
        comp = re.sub(r"[^a-z0-9]", "", (self.company or "").lower())
        title = re.sub(r"[^a-z0-9]", "", self.title.lower())[:40]
        core = f"{title}|{comp}|{self.country.lower()}"
        return hashlib.sha1(core.encode("utf-8")).hexdigest()[:20]

    def dedup_key(self, company_alias: str | None = None) -> str:
        """Normalized identity used by the deduplication layer.

        Prefer external_id when present and stable; otherwise fall back to a
        normalized title + company hash so cross-source duplicates collapse.
        """
        comp = (company_alias or self.company or "").strip().lower()
        comp = re.sub(r"[^a-z0-9]", "", comp)
        title = re.sub(r"[^a-z0-9]", "", self.title.lower())[:40]
        if self.external_id and not self.external_id.lower().startswith(("http", "www")):
            return f"{self.source}:{self.external_id}"
        core = f"{title}|{comp}|{self.country.lower()}"
        return f"hash:{hashlib.sha1(core.encode('utf-8')).hexdigest()[:16]}"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["posted_at"] = self.posted_at.isoformat() if self.posted_at else None
        d["closing_at"] = self.closing_at.isoformat() if self.closing_at else None
        return d


def source_type_for(source_name: str) -> str:
    """Best-effort source_type from a connector/source name."""
    n = source_name.lower()
    if any(k in n for k in ("eures", "gov", "government", "national", "emploi", "pole")):
        return "government"
    if any(k in n for k in ("greenhouse", "lever", "smartrecruit", "workday", "icims", "successfactor", "ats")):
        return "ats"
    if any(k in n for k in ("company", "career", "careers", "employer")):
        return "company_career"
    if any(k in n for k in ("agency", "recruit", "groupe", "interim")):
        return "recruitment"
    if any(k in n for k in ("search", "duckduckgo", "google", "bing")):
        return "search_engine"
    if any(k in n for k in ("linkedin", "facebook", "meta")):
        return "social_signal"
    if any(k in n for k in ("indeed", "jobboard", "job_", "jobs", "stepstone", "monster", "wuzzuf")):
        return "job_board"
    if any(k in n for k in ("immigration", "visa", "work permit")):
        return "immigration"
    return "unknown"


@runtime_checkable
class JobSource(Protocol):
    """Contract every discovery connector implements."""

    name: str
    kind: str  # 'rss' | 'api' | 'html' | 'html_search'
    # §2 source category (see SOURCE_TYPES); derived from name when absent.
    source_type: str
    # §34 — how this connector may legally access its source (see ACCESS_MODES).
    access_mode: str = "public"
    policy_notice: str = ""

    async def search(self, query: str, location: str = "") -> list[Opportunity]:
        ...


registry: dict[str, type[JobSource]] = {}


def get_connector(kind: str, **kwargs) -> JobSource:
    if kind not in registry:
        raise KeyError(f"Unknown connector kind {kind!r}. Registered: {sorted(registry)}")
    return registry[kind](**kwargs)


def parse_date(value: str | None) -> datetime | None:
    """Parse a few common date formats; returns an aware UTC datetime or None."""
    if not value:
        return None
    value = value.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d", "%a, %d %b %Y %H:%M:%S %z"):
        try:
            dt = datetime.strptime(value, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None
