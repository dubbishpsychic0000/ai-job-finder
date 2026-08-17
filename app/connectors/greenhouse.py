"""Greenhouse ATS job-board connector (spec §2, §8).

Uses Greenhouse's *public* job-board JSON API (`boards.greenhouse.io`):
no credentials, no scraping, officially intended for embedding job lists on
career sites. Company board tokens are configured in sources.yaml.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import requests

from app.connectors.base import Opportunity, infer_country, parse_date

logger = logging.getLogger(__name__)
USER_AGENT = "WorldwideCareerAgent/0.1 (public boards.greenhouse.io API)"
API_TMPL = "https://boards.greenhouse.io/{token}/jobs"


def _clean_html(text: str) -> str:
    from bs4 import BeautifulSoup

    return BeautifulSoup(text or "", "html.parser").get_text(" ", strip=True)


def _matches(title: str, query: str) -> bool:
    words = [w for w in query.lower().split() if len(w) > 3]
    if not words:
        return True
    tl = title.lower()
    return any(w in tl for w in words) or words[0] in tl


class GreenhouseSource:
    """Jobs from Greenhouse board tokens via their public API."""

    name = "greenhouse"
    kind = "api"
    source_type = "ats"
    access_mode = "public"
    policy_notice = "Uses Greenhouse's public job-board API (boards.greenhouse.io). No auth, no scraping."

    def __init__(self, boards: list[str] | None = None, data_path: str | Path | None = None):
        self.boards = boards or []
        # Hermetic testing: read a local JSON file shaped like the API response.
        self.data_path = Path(data_path) if data_path else None

    def _fetch(self, token: str) -> dict:
        if self.data_path:
            return json.loads(self.data_path.read_text(encoding="utf-8"))
        url = API_TMPL.format(token=token)
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
        resp.raise_for_status()
        return resp.json()

    def _from_job(self, token: str, job: dict) -> Opportunity:
        loc = (job.get("location") or {}).get("name", "")
        return Opportunity(
            source=f"greenhouse:{token}",
            source_type="ats",
            external_id=f"greenhouse:{token}:{job.get('id')}",
            title=job.get("title", ""),
            company=job.get("company_name", "") or token,
            location=loc,
            country=infer_country(loc),
            description=_clean_html(job.get("content", ""))[:20000],
            url=job.get("absolute_url", ""),
            posted_at=parse_date(job.get("updated_at")),
            employment_type="full_time",
            raw={"board": token, "ats": "greenhouse"},
        )

    async def search(self, query: str, location: str = "") -> list[Opportunity]:
        out: list[Opportunity] = []
        for token in self.boards:
            try:
                payload = self._fetch(token)
            except Exception as exc:
                logger.warning("Greenhouse board %s failed: %s", token, exc)
                continue
            for job in payload.get("jobs", []):
                title = job.get("title", "")
                if title and query and not _matches(title, query):
                    continue
                out.append(self._from_job(token, job))
        return out
