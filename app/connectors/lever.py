"""Lever ATS job-board connector (spec §2, §8).

Uses Lever's *public* postings JSON API (`api.lever.co/v0/postings/{site}`)
which is the same interface career pages use to embed their job board — no
credentials and no scraping. Company site slugs are configured in sources.yaml.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import requests

from app.connectors.base import Opportunity, infer_country

logger = logging.getLogger(__name__)
USER_AGENT = "WorldwideCareerAgent/0.1 (public Lever postings API)"
API_TMPL = "https://api.lever.co/v0/postings/{site}?mode=json"


def _matches(title: str, query: str) -> bool:
    words = [w for w in query.lower().split() if len(w) > 3]
    if not words:
        return True
    tl = title.lower()
    return any(w in tl for w in words) or words[0] in tl


def _ms_epoch(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


class LeverSource:
    """Jobs from Lever site slugs via their public postings API."""

    name = "lever"
    kind = "api"
    source_type = "ats"
    access_mode = "public"
    policy_notice = "Uses Lever's public postings JSON API. No auth, no scraping."

    def __init__(self, sites: list[str] | None = None, data_path: str | Path | None = None):
        self.sites = sites or []
        self.data_path = Path(data_path) if data_path else None

    def _fetch(self, site: str) -> list:
        if self.data_path:
            payload = json.loads(self.data_path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, list) else []
        resp = requests.get(API_TMPL.format(site=site),
                            headers={"User-Agent": USER_AGENT}, timeout=20)
        resp.raise_for_status()
        payload = resp.json()
        return payload if isinstance(payload, list) else []

    def _from_posting(self, site: str, posting: dict) -> Opportunity:
        loc = (posting.get("categories") or {}).get("location", "") or ""
        title = posting.get("text", "")
        return Opportunity(
            source=f"lever:{site}",
            source_type="ats",
            external_id=f"lever:{site}:{posting.get('id', '')}",
            title=title,
            company=posting.get("company") or site,
            location=loc,
            country=infer_country(loc),
            description=(posting.get("descriptionPlain") or posting.get("description", ""))[:20000],
            url=posting.get("hostedUrl") or posting.get("applyUrl") or "",
            posted_at=_ms_epoch(posting.get("createdAt")),
            employment_type="full_time",
            raw={"site": site, "ats": "lever"},
        )

    async def search(self, query: str, location: str = "") -> list[Opportunity]:
        out: list[Opportunity] = []
        for site in self.sites:
            try:
                payload = self._fetch(site)
            except Exception as exc:
                logger.warning("Lever site %s failed: %s", site, exc)
                continue
            for posting in payload:
                if not isinstance(posting, dict):
                    continue
                title = posting.get("text", "")
                if title and query and not _matches(title, query):
                    continue
                out.append(self._from_posting(site, posting))
        return out
