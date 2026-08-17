"""SmartRecruiters ATS job-board connector (spec §2, §8).

Uses SmartRecruiters' *public* postings API (`api.smartrecruiters.com`) which
requires no API key for a company's published postings. Company IDs are
configured in sources.yaml.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import requests

from app.connectors.base import Opportunity, infer_country, parse_date

logger = logging.getLogger(__name__)
USER_AGENT = "WorldwideCareerAgent/0.1 (public SmartRecruiters postings API)"
API_TMPL = "https://api.smartrecruiters.com/v1/companies/{cid}/postings"
DETAIL_TMPL = "https://jobs.smartrecruiters.com/{cid}/{pid}"


def _matches(title: str, query: str) -> bool:
    words = [w for w in query.lower().split() if len(w) > 3]
    if not words:
        return True
    tl = title.lower()
    return any(w in tl for w in words) or words[0] in tl


class SmartRecruitersSource:
    """Jobs from SmartRecruiters companies via their public postings API."""

    name = "smartrecruiters"
    kind = "api"
    source_type = "ats"
    access_mode = "public"
    policy_notice = "Uses SmartRecruiters' public postings API. No auth, no scraping."

    def __init__(self, companies: list[str] | None = None, data_path: str | Path | None = None):
        self.companies = companies or []
        self.data_path = Path(data_path) if data_path else None

    def _fetch(self, cid: str) -> dict:
        if self.data_path:
            return json.loads(self.data_path.read_text(encoding="utf-8"))
        url = API_TMPL.format(cid=cid)
        resp = requests.get(url, headers={"User-Agent": USER_AGENT, "accept": "application/json"}, timeout=20)
        resp.raise_for_status()
        return resp.json()

    def _from_posting(self, cid: str, posting: dict) -> Opportunity:
        loc = (posting.get("location") or {}).get("city", "")
        country = infer_country(loc) or (posting.get("location") or {}).get("country", "")
        comp = (posting.get("company") or {}).get("name", "") or cid
        pid = posting.get("id", "")
        return Opportunity(
            source=f"smartrecruiters:{cid}",
            source_type="ats",
            external_id=f"smartrecruiters:{cid}:{pid}",
            title=posting.get("name", ""),
            company=comp,
            location=loc,
            country=country,
            description="",
            url=DETAIL_TMPL.format(cid=cid, pid=pid),
            posted_at=parse_date(posting.get("releasedDate")),
            employment_type=(posting.get("typeOfEmployment") or "").lower() or "full_time",
            raw={"company_id": cid, "ats": "smartrecruiters", "ref": posting.get("ref", "")},
        )

    async def search(self, query: str, location: str = "") -> list[Opportunity]:
        out: list[Opportunity] = []
        for cid in self.companies:
            try:
                payload = self._fetch(cid)
            except Exception as exc:
                logger.warning("SmartRecruiters company %s failed: %s", cid, exc)
                continue
            for posting in payload.get("content", []):
                title = posting.get("name", "")
                if title and query and not _matches(title, query):
                    continue
                out.append(self._from_posting(cid, posting))
        return out
