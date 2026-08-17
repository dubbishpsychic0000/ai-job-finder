"""Meta (Facebook) opportunity signals (spec §17, §18) — user-provided ONLY (§34).

Meta/Facebook has no public job index and no general jobs API accessible without
App Review + a user's authorization. The only authorized channel for this agent
is the USER: group/page URLs or pasted announcements the user hands over.

This connector therefore:
  * REJECTS any config whose `access_mode` is not `user_provided`;
  * performs ZERO network access — it only emits the leads the user supplied;
  * marks every result `verification_status="unverified"`.

That makes it structurally impossible for the pipeline to scrape facebook.com.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from app.connectors.base import Opportunity, infer_country

logger = logging.getLogger(__name__)

FACEBOOK_HOST_MARK = "facebook.com"
META_HOST_MARKS = (FACEBOOK_HOST_MARK, "fb.com")


def _slug_title(url: str, fallback: str) -> str:
    for mark in META_HOST_MARKS:
        if mark in (url or "").lower():
            path = url.split(f"{mark}", 1)[-1].strip("/")
            if path:
                return path.replace("/", " ").replace("-", " ").title()[:200]
    return (fallback or "Facebook/Meta group announcement")[:200]


class MetaJobsSource:
    """User-provided Meta/Facebook group & page signals. Never fetches the network."""

    name = "meta_fb_groups"
    kind = "html_search"
    source_type = "social_signal"
    access_mode = "user_provided"
    policy_notice = (
        "user-provided Facebook/Meta group, page URLs or pasted announcements "
        "only (spec §34). access_mode must be 'user_provided'; this connector "
        "never performs network access."
    )

    def __init__(self, config: dict[str, Any] | None = None, *, urls=None, leads=None):
        self.config = config or {}
        mode = self.config.get("access_mode", "user_provided")
        if mode != "user_provided":
            raise ValueError(
                f"MetaJobsSource requires access_mode 'user_provided' (no Facebook "
                f"scraping permitted, spec §34); got {mode!r}")
        self.access_mode = mode
        self.country = self.config.get("country", "")
        self.urls = [u for u in (urls or self.config.get("urls") or []) if u]
        self.leads = leads or self.config.get("leads") or []

    async def search(self, query: str = "", location: str = "") -> list[Opportunity]:
        out: list[Opportunity] = []
        for u in self.urls:
            out.append(self._from_url(u, location))
        for i, lead in enumerate(self.leads):
            out.append(self._from_lead(lead, i, location))
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
