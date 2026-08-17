"""iCIMS ATS job-board connector (spec §2, §8).

Uses iCIMS' *public* career-site search endpoint (`...icims.com/jobs/search`
with `ss=1`, which returns JSON — the same interface career pages use). No
credentials, no scraping. Hosts are configured in sources.yaml. Parsing is
deliberately defensive: if iCIMS ever changes the payload shape we degrade to
an empty result instead of failing discovery.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

from app.connectors.base import Opportunity, infer_country, parse_date

logger = logging.getLogger(__name__)
USER_AGENT = "WorldwideCareerAgent/0.1 (public iCIMS careers search API)"
SEARCH_TMPL = "https://{host}/jobs/search?ss=1&in_iframe=1&searchQuery={query}"


def _matches(title: str, query: str) -> bool:
    words = [w for w in query.lower().split() if len(w) > 3]
    if not words:
        return True
    tl = title.lower()
    return any(w in tl for w in words) or words[0] in tl


def _iter_jobs(payload: Any):
    sr = (payload or {}).get("searchResults") or {}
    for entry in sr.get("jobs", []):
        job = entry.get("job", {}) if isinstance(entry, dict) else {}
        if isinstance(job, dict):
            yield job


class ICIMSSource:
    """Jobs from iCIMS career hosts via their public JSON search endpoint."""

    name = "icims"
    kind = "api"
    source_type = "ats"
    access_mode = "public"
    policy_notice = "Uses iCIMS' public careers search API. No auth, no scraping."

    def __init__(self, hosts: list[str] | None = None, data_path: str | Path | None = None):
        self.hosts = hosts or []
        self.data_path = Path(data_path) if data_path else None

    def _fetch(self, host: str, query: str) -> dict:
        if self.data_path:
            return json.loads(self.data_path.read_text(encoding="utf-8"))
        url = SEARCH_TMPL.format(host=host, query=quote(query))
        resp = requests.get(url,
                            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                            timeout=20)
        resp.raise_for_status()
        return resp.json()

    def _from_job(self, host: str, job: dict) -> Opportunity:
        loc_obj = job.get("location") or {}
        loc = ", ".join(p for p in (loc_obj.get("city", ""), loc_obj.get("countryCode", "")) if p)
        url = job.get("jobUrl") or job.get("jobLink") or ""
        return Opportunity(
            source=f"icims:{host}",
            source_type="ats",
            external_id=f"icims:{host}:{job.get('id') or url}",
            title=job.get("title") or job.get("jobTitle", ""),
            company=job.get("company") or job.get("companyName") or "",
            location=loc,
            country=infer_country(loc),
            description="",
            url=url,
            posted_at=parse_date(job.get("postedDate") or job.get("datePosted")),
            employment_type="full_time",
            raw={"ats": "icims", "host": host},
        )

    async def search(self, query: str, location: str = "") -> list[Opportunity]:
        out: list[Opportunity] = []
        for host in self.hosts:
            try:
                payload = self._fetch(host, query)
            except Exception as exc:
                logger.warning("iCIMS host %s failed: %s", host, exc)
                continue
            for job in _iter_jobs(payload):
                title = job.get("title") or job.get("jobTitle", "")
                if title and query and not _matches(title, query):
                    continue
                out.append(self._from_job(host, job))
        return out
