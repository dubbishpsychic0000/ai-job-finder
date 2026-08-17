"""Generic RSS/Atom connector (Priority 2: structured feeds).

Many job boards and government job services expose RSS search feeds (e.g.
USAJobs, EURES, national government feeds, recruiting professionals). This
adapter normalizes RSS items into Opportunities.

Feed behaviour helpers `feeds` — a list of URLs. Each URL may include
{query} and {location} placeholders that the search planner substitutes.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

import feedparser
import requests

from app.connectors.base import Opportunity, infer_country, parse_date

logger = logging.getLogger(__name__)

USER_AGENT = "WorldwideCareerAgent/0.1 (+educational agent; respects site terms)"


def _clean_html(text: str) -> str:
    from bs4 import BeautifulSoup

    return BeautifulSoup(text or "", "html.parser").get_text(" ", strip=True)


class RSSJobSource:
    """Adapter that turns a set of RSS/Atom feeds into Opportunities."""

    name = "rss"
    kind = "rss"

    def __init__(self, feeds: list[str] | None = None, base_type: str = "jobs"):
        self.feeds = feeds or []
        self.base_type = base_type

    def _fetch_feed(self, url: str, timeout: int = 20) -> Any:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
        resp.raise_for_status()
        return feedparser.parse(resp.content)

    def _external_id(self, entry: Any, url: str) -> str:
        for key in ("id", "link", "guid"):
            value = getattr(entry, key, None)
            if value:
                return str(value)
        return url

    def _from_entry(self, entry: Any, feed_url: str, query: str = "") -> Opportunity:
        title = _clean_html(getattr(entry, "title", "") or query)
        link = getattr(entry, "link", "") or ""
        summary = _clean_html(getattr(entry, "summary", "") or getattr(entry, "description", "") or "")
        published = getattr(entry, "published", "") or getattr(entry, "updated", "")

        # Many job RSS feeds pack "Title — Company — Location" into the title
        company = ""
        location = ""
        parts = [p.strip() for p in re.split(r"\s+[-–—|·]\s+", title)]
        if len(parts) >= 3:
            title, company, location = parts[0], parts[1], " - ".join(parts[2:])
        elif len(parts) == 2:
            # ambiguous — keep the title, treat the rest as location hint
            location = parts[-1]

        try:
            etype = ""
            for tag in getattr(entry, "tags", []) or []:
                t = str(tag.get("term", "")).lower()
                if "contract" in t or "permanent" in t or "full" in t or "part" in t:
                    etype = t.replace(" ", "_")
        except Exception:
            etype = ""

        posted = parse_date(published) or datetime.now(timezone.utc)
        return Opportunity(
            source=f"rss:{self.name}",
            external_id=self._external_id(entry, link),
            title=title,
            company=company,
            location=location,
            country=infer_country(location),
            description=summary[:20000],
            url=link,
            posted_at=posted,
            employment_type=etype or "full_time",
            salary=None,
            contact_email=None,
            raw={"feed": feed_url, "query": query},
        )

    async def search(self, query: str, location: str = "") -> list[Opportunity]:
        out: list[Opportunity] = []
        if not self.feeds:
            return out
        for feed in self.feeds:
            url = feed.format(query=query.replace(" ", "+"), location=location.replace(" ", "+"))
            try:
                parsed = self._fetch_feed(url)
                for entry in parsed.entries:
                    opp = self._from_entry(entry, url, query=query)
                    if opp.title and opp.url and _title_contains(opp.title, query):
                        out.append(opp)
            except Exception as exc:  # one bad feed must not kill discovery
                logger.warning("RSS feed %s failed: %s", url, exc)
        return out


def _title_contains(title: str, query: str) -> bool:
    words = [w for w in query.lower().split() if len(w) > 3]
    if not words:
        return True
    tl = title.lower()
    return any(w in tl for w in words) or words[0] in tl
