"""Workday ATS job-board connector (spec §2, §8).

Uses Workday's *public* career-site job-search API (the same POST endpoint
`myworkdayjobs.com` career pages use to load their boards). No credentials, no
scraping. Each company is configured in sources.yaml with its Workday host,
tenant and company code.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import requests

from app.connectors.base import Opportunity, infer_country, parse_date

logger = logging.getLogger(__name__)
USER_AGENT = "WorldwideCareerAgent/0.1 (public Workday job search API)"


def _clean_html(text: str) -> str:
    from bs4 import BeautifulSoup

    return BeautifulSoup(text or "", "html.parser").get_text(" ", strip=True)


def _matches(title: str, query: str) -> bool:
    words = [w for w in query.lower().split() if len(w) > 3]
    if not words:
        return True
    tl = title.lower()
    return any(w in tl for w in words) or words[0] in tl


class WorkdaySource:
    """Jobs from Workday companies via their public career-site search API."""

    name = "workday"
    kind = "api"
    source_type = "ats"
    access_mode = "public"
    policy_notice = "Uses Workday's public career-site job search API. No auth, no scraping."

    def __init__(self, companies: list[dict[str, Any]] | None = None,
                 data_path: str | Path | None = None):
        self.companies = companies or []
        self.data_path = Path(data_path) if data_path else None

    def _fetch(self, cfg: dict[str, Any], query: str) -> dict:
        if self.data_path:
            return json.loads(self.data_path.read_text(encoding="utf-8"))
        url = f"https://{cfg['host']}/wday/cxs/{cfg['tenant']}/{cfg['company']}/jobs"
        body: dict[str, Any] = {"appliedFacets": {}, "limit": 20, "offset": 0}
        if query:
            body["searchText"] = query
        resp = requests.post(url, json=body,
                             headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                             timeout=20)
        resp.raise_for_status()
        return resp.json()

    def _from_posting(self, cfg: dict[str, Any], posting: dict) -> Opportunity:
        loc = posting.get("locationsText", "") or ""
        path = posting.get("externalPath", "") or ""
        host = cfg.get("host", "")
        url = f"https://{host}{path}" if path else ""
        return Opportunity(
            source=f"workday:{cfg.get('name') or host}",
            source_type="ats",
            external_id=f"workday:{host}:{posting.get('id') or path}",
            title=posting.get("title", ""),
            company=cfg.get("display_name") or cfg.get("company", ""),
            location=loc,
            country=infer_country(loc),
            description=_clean_html(posting.get("jobDescription", ""))[:20000],
            url=url,
            posted_at=parse_date(posting.get("postedOn")),
            employment_type="full_time",
            raw={"ats": "workday", "host": host},
        )

    async def search(self, query: str, location: str = "") -> list[Opportunity]:
        out: list[Opportunity] = []
        for cfg in self.companies:
            try:
                payload = self._fetch(cfg, query)
            except Exception as exc:
                logger.warning("Workday company %s failed: %s", cfg.get("host"), exc)
                continue
            for posting in payload.get("jobPostings", []):
                if not isinstance(posting, dict):
                    continue
                title = posting.get("title", "")
                if title and query and not _matches(title, query):
                    continue
                out.append(self._from_posting(cfg, posting))
        return out
