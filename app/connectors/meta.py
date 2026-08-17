"""Meta (Facebook) opportunity signals (spec §17, §18) — authorized channels only (§34).

Meta/Facebook has no public job index and no general jobs API accessible without
App Review + a user's authorization. The channels permitted by the spec are:

  1. the public search-engine index — `site:facebook.com` results fetched through
     the (injectable, non-login) search engine; the connector itself NEVER fetches
     facebook.com directly, it only receives what the engine reports;
  2. the USER: group/page URLs or pasted announcements the user hands over.

The two modes map to those channels:
  * `access_mode: public`        — index channel (search_fn injectable),
  * `access_mode: user_provided` — user URLs/pasted leads only, zero network.

Everything this connector emits is marked `verification_status="unverified"` and
its `raw.channel` records which authorized channel surfaced it.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from app.connectors.base import Opportunity, infer_country

logger = logging.getLogger(__name__)

FACEBOOK_HOST_MARK = "facebook.com"
META_HOST_MARKS = (FACEBOOK_HOST_MARK, "fb.com")
ALLOWED_MODES = ("public", "user_provided")


def _slug_title(url: str, fallback: str) -> str:
    for mark in META_HOST_MARKS:
        if mark in (url or "").lower():
            path = url.split(f"{mark}", 1)[-1].strip("/")
            if path:
                return path.replace("/", " ").replace("-", " ").title()[:200]
    return (fallback or "Facebook/Meta group announcement")[:200]


class MetaJobsSource:
    """Meta/Facebook signals via the public search index + user-provided leads.

    `public` mode runs `site:facebook.com` through the injected search engine and
    filters its results to facebook.com/fb.com hosts — the connector never opens a
    direct connection to Meta. `user_provided` mode performs no network access at
    all. Access-mode violations are rejected at construction (§34).
    """

    name = "meta_fb_groups"
    kind = "html_search"
    source_type = "social_signal"
    access_mode = "user_provided"
    policy_notice = (
        "Authorized channels only (spec §34): public search-engine index results "
        "for site:facebook.com plus URLs/announcements the user provides. No "
        "login, no session, no direct access to facebook.com."
    )

    def __init__(self, config: dict[str, Any] | None = None, *, urls=None, leads=None,
                 search_fn=None):
        self.config = config or {}
        mode = self.config.get("access_mode", "user_provided")
        if mode not in ALLOWED_MODES:
            raise ValueError(
                f"MetaJobsSource rejects access_mode {mode!r} (spec §34); "
                f"allowed: {ALLOWED_MODES}")
        self.access_mode = mode
        self.country = self.config.get("country", "")
        self.search_fn = search_fn or _meta_index_search
        self.urls = [u for u in (urls or self.config.get("urls") or []) if u]
        self.leads = leads or self.config.get("leads") or []

    async def search(self, query: str = "", location: str = "") -> list[Opportunity]:
        out: list[Opportunity] = []
        if self.access_mode == "public":
            try:
                out.extend(await self._index(query, location))
            except Exception as exc:
                logger.warning("meta index search failed: %s", exc)
        for u in self.urls:
            if any(mark in (u or "").lower() for mark in META_HOST_MARKS):
                out.append(self._from_url(u, location))
        for i, lead in enumerate(self.leads):
            out.append(self._from_lead(lead, i, location))
        return out

    async def _index(self, query: str, location: str) -> list[Opportunity]:
        results = await self.search_fn(f"site:{FACEBOOK_HOST_MARK} {query}", location) or []
        out: list[Opportunity] = []
        for item in results:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url", "") or "")
            if not any(mark in url.lower() for mark in META_HOST_MARKS):
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
                country=infer_country(location) or self.country,
                description=str(item.get("snippet", "") or "")[:20000],
                url=url,
                verification_status="unverified",
                raw={"platform": "meta", "channel": "search_engine_index"},
            ))
        return out

    def _from_url(self, url: str, location: str) -> Opportunity:
        loc = location or self.country
        return Opportunity(
            source=self.name,
            source_type="social_signal",
            external_id=url,
            title=_slug_title(url, "Facebook/Meta group posting"),
            location=loc,
            country=infer_country(loc) or self.country,
            url=url,
            verification_status="unverified",
            raw={"platform": "meta", "channel": "user_provided"},
        )

    def _from_lead(self, lead: dict[str, Any], index: int, location: str) -> Opportunity:
        loc = lead.get("location") or location or self.country
        title = str(lead.get("title") or "")[:200]
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40] or f"lead-{index}"
        url = lead.get("url") or f"meta://user-lead/{slug}"
        return Opportunity(
            source=self.name,
            source_type="social_signal",
            external_id=url,
            title=title,
            location=loc,
            country=infer_country(loc) or self.country,
            description=str(lead.get("note") or "")[:20000],
            url=url,
            verification_status="unverified",
            raw={"platform": "meta", "channel": "user_provided_lead"},
        )


async def _meta_index_search(query: str, location: str = "") -> list[dict]:
    """§18 default index channel — service:facebook.com via the search engine."""
    from app.connectors.search_engine import SearchEngineSource

    ops = await SearchEngineSource(results_per_query=8).search(query, location)
    return [{"url": o.url, "title": o.title, "snippet": o.description} for o in ops if o.url]
