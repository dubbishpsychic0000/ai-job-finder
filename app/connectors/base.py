"""Connector interface and the normalized Opportunity format.

Adapted from the spec:

    {
      "source": "example",
      "external_id": "12345",
      "title": "Civil Engineering Technician",
      "company": "Example Construction",
      "location": "France",
      "description": "...",
      "url": "...",
      "posted_at": "...",
      "employment_type": "full_time",
      "salary": null,
      "contact_email": null
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
        return d


@runtime_checkable
class JobSource(Protocol):
    """Contract every discovery connector implements."""

    name: str
    kind: str  # 'rss' | 'api' | 'html' | 'html_search'

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
