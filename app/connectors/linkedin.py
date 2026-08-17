"""LinkedIn opportunity signals (spec §15, §18) — AUTHORIZED paths only (spec §34).

LinkedIn restricts automated access. This connector therefore NEVER touches
linkedin.com directly: no logins, no session cookies, no scraping. It uses the
two authorized channels permitted for protected platforms:

  1. the public search-engine index — `site:linkedin.com/jobs` results fetched
     through the (injectable) DuckDuckGo connector, which itself never logs in;
  2. job URLs the user explicitly provided in config (`urls`) — surfaced as
     unverified leads for the user to confirm.

Every result is `verification_status="unverified"` because a social listing can
only be confirmed through the index (or by the user), never via a direct
automated fetch of the platform. If a source declares `access_mode:
user_provided`, the index channel is suppressed and only the user's URLs are
used.
"""
from __future__ import annotations

import logging
import re
import urllib.parse
from typing import Any

from app.connectors.base import Opportunity, infer_country
from app.connectors.search_engine import SearchEngineSource

logger = logging.getLogger(__name__)

LINKEDIN_HOST_MARK = "linkedin.com"
ALLOWED_MODES = ("public", "user_provided")


def _slug_title(url: str) -> str:
    """Best-effort human-readable title from a LinkedIn job URL path."""
    m = re.search(r"/jobs/view/(\d+)", url or "", re.I)
    if m:
        return f"LinkedIn job #{m.group(1)}"
    parts = [p for p in urllib.parse.urlparse(url).path.split("/") if p and p.lower() != "jobs"]
    last = parts[-1].replace("-", " ").title() if parts else url
    return (last or url)[:200]


class LinkedInJobsSource:
    """LinkedIn signals via the public search index + user-provided URLs only."""

    name = "linkedin_jobs"
    kind = "html_search"
    source_type = "social_signal"
    access_mode = "public"
    policy_notice = (
        "Authorized paths only (spec §34): public search-engine index results for "
        "site:linkedin.com/jobs plus URLs the user provides. No login, no session "
        "cookies, no direct scraping of linkedin.com."
    )

    def __init__(self, config: dict[str, Any] | None = None, *, urls=None, search_fn=None):
        self.config = config or {}
        mode = self.config.get("access_mode", "public")
        if mode not in ALLOWED_MODES:
            raise ValueError(
                f"LinkedInJobsSource rejects access_mode {mode!r} (spec §34); "
                f"allowed: {ALLOWED_MODES}")
        self.access_mode = mode
        self.urls = [u for u in (urls or self.config.get("urls") or []) if u]
        self.search_fn = search_fn or _index_search
        for u in self.urls:
            if LINKEDIN_HOST_MARK not in (u or "").lower():
                logger.warning("linkedin_jobs: ignoring non-linkedin user URL %r", u)

    async def search(self, query: str, location: str = "") -> list[Opportunity]:
        out: list[Opportunity] = []
        if self.access_mode == "public":
            try:
                out.extend(await self._index(query, location))
            except Exception as exc:
                logger.warning("linkedin index search failed: %s", exc)
        for u in self.urls:
            if LINKEDIN_HOST_MARK in u.lower():
                out.append(self._from_url(u, location))
        return out

    async def _index(self, query: str, location: str) -> list[Opportunity]:
        results = await self.search_fn(f"site:{LINKEDIN_HOST_MARK}/jobs {query}",
                                       location) or []
        out: list[Opportunity] = []
        for item in results:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url", "") or "")
            if LINKEDIN_HOST_MARK not in url.lower():
                continue
            title = str(item.get("title", "") or "")[:200]
            if not title:
                continue
            out.append(Opportunity(
                source=self.name,
                source_type="social_signal",
                external_id=url,
                title=title,
                location=location,
                country=infer_country(location),
                description=str(item.get("snippet", "") or "")[:20000],
                url=url,
                verification_status="unverified",
                raw={"platform": "linkedin", "channel": "search_engine_index"},
            ))
        return out

    def _from_url(self, url: str, location: str) -> Opportunity:
        return Opportunity(
            source=self.name,
            source_type="social_signal",
            external_id=url,
            title=_slug_title(url),
            location=location,
            country=infer_country(location),
            url=url,
            verification_status="unverified",
            raw={"platform": "linkedin", "channel": "user_provided"},
        )


async def _index_search(query: str, location: str = "") -> list[dict]:
    ops = await SearchEngineSource(results_per_query=8).search(query, location)
    return [{"url": o.url, "title": o.title, "snippet": o.description} for o in ops if o.url]
