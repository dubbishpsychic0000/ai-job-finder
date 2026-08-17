"""Normalization layer.

Connectors already emit `Opportunity` objects, but this pass is the last
authoritative cleanup before the DB: drop junk, verify URLs, truncate huge
descriptions, backfill country, and clamp posted dates.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import urlparse

from app.connectors.base import Opportunity, infer_country

_MIN_TITLE = 3
_MAX_DESCRIPTION = 20000  # storage / token-budget safety


def is_valid_url(url: str) -> bool:
    try:
        p = urlparse(url)
        return p.scheme in ("http", "https") and bool(p.netloc)
    except ValueError:
        return False


def normalize(opp: Opportunity, *, now: datetime | None = None) -> Opportunity | None:
    """Return a cleaned copy, or None when the opportunity must be dropped."""
    now = now or datetime.now(timezone.utc)
    title = re.sub(r"\s+", " ", (opp.title or "")).strip()
    if len(title) < _MIN_TITLE:
        return None
    if not is_valid_url(opp.url):
        return None
    if opp.posted_at is None or opp.posted_at.timestamp() > now.timestamp() + 86400:
        from app.connectors.base import parse_date

        raw = opp.raw.get("posted") if isinstance(opp.raw, dict) else None
        opp.posted_at = parse_date(str(raw)) if raw else now
    if not opp.country and opp.location:
        opp.country = infer_country(opp.location)
    opp.description = (opp.description or "")[:_MAX_DESCRIPTION]
    opp.title = title
    opp.company = (opp.company or "").strip()
    opp.location = (opp.location or "").strip()
    return opp
